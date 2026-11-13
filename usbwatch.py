#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盯板子自己的 USB 口，手机插上/拔下就触发。

为什么不用 adb 轮询：
  1. 慢。轮询一秒一次，插拔那一瞬间可能错过；udev 是内核直接通知，毫秒级。
  2. 脆。走 adb 就得先有可用连接。WiFi 一断、USB 正在枚举，全查不到。
     这次实测就踩过：手机 WiFi 被关，`/power` 全 500，拔插检测彻底失效。
  3. 准。板子看到的是"这条线通没通"，不是手机自报的电池状态。

板子侧看手机：/sys/bus/usb/devices/*/  里 idVendor=18d1（Google/ADB），
再按 serial 匹配（默认 892e20c9）。拔了就整个目录消失，插上就出现。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
USB_ROOT = "/sys/bus/usb/devices"
SERIAL = os.environ.get("PHONE_SERIAL", "892e20c9")
VENDOR = "18d1"          # Google —— adb 接口的手机都报这个
DEBOUNCE = float(os.environ.get("USB_DEBOUNCE", "2.5"))
COOLDOWN = float(os.environ.get("USB_COOLDOWN", "12"))


def log(msg):
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "%s %s" % (t, msg)
    print(line, flush=True)


def find_phone():
    """手机现在插着吗。返回 sysfs 路径或 None。"""
    try:
        names = os.listdir(USB_ROOT)
    except OSError:
        return None
    for n in names:
        d = os.path.join(USB_ROOT, n)
        try:
            with open(os.path.join(d, "idVendor")) as f:
                if f.read().strip() != VENDOR:
                    continue
            sn = ""
            p = os.path.join(d, "serial")
            if os.path.exists(p):
                with open(p) as f:
                    sn = f.read().strip()
            # 有线 adb 的 serial 就是手机序列号；匹配不上也认（换线不影响）
            if SERIAL and sn and sn != SERIAL:
                continue
            return d
        except OSError:
            continue
    return None


def sh(serial, cmd, timeout=20):
    """对某台设备发一条 shell 命令，超时就返回空。"""
    try:
        r = subprocess.run(["adb", "-s", serial, "shell", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def any_device():
    """现在 adb 能看到哪台（USB 序列号或无线都行）。"""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=15).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def wait_android_boot(max_wait=240):
    """等手机真的开机完成，而不是"USB 刚插上"。

    用户的真实场景：手机每次都关机，给板子通电后手机靠 USB 供电自动开机。
    手机从关机到进桌面要几十秒，这期间 USB 设备早就枚举出来了
    （`/sys` 里已经有节点），但 Android 根本没起来 —— 这时候跑流程
    只会全盘失败。所以必须先等到 sys.boot_completed=1。
    """
    t0 = time.time()
    last_log = 0
    while time.time() - t0 < max_wait:
        dev = any_device()
        if dev:
            boot = sh(dev, "getprop sys.boot_completed", timeout=12)
            if boot.strip() == "1":
                el = time.time() - t0
                if el > 3:
                    log("手机已开机完成（等了 %.0fs）" % el)
                return True
        el = time.time() - t0
        if el - last_log >= 15:
            log("等手机开机… 已等 %.0fs%s" % (el, "（adb 还没看到设备）" if not dev else ""))
            last_log = el
        time.sleep(3)
    log("等开机超时（%.0fs），先试着跑流程" % max_wait)
    return False


def battery_level(serial=None):
    """读电量百分比，读不到返回 None。"""
    dev = serial or any_device()
    if not dev:
        return None
    out = sh(dev, "dumpsys battery | grep level", timeout=12)
    for line in out.splitlines():
        if "level" in line:
            try:
                return int(line.split(":")[1].strip())
            except (IndexError, ValueError):
                return None
    return None


def battery_ok(min_level=None):
    """电量够不够跑一轮。

    板子 USB 口只给 5V/0.5A，手机跑流程时是净放电。电量太低时跑到一半
    会直接关机（实测发生过：25% 电量，第 4 步掉线，随后进 05c6 关机模式）。
    """
    floor = int(min_level if min_level is not None
                else os.environ.get("MIN_BATTERY", "20"))
    lv = battery_level()
    if lv is None:
        return True, "电量读不到，照跑"
    if lv < floor:
        return False, "电量 %d%%，低于 %d%%，先不跑（USB 只有 500mA，会跑到一半关机）" % (
            lv, floor)
    return True, "电量 %d%%" % lv


def wait_system_ready_here(max_wait=180, settle=15):
    """等系统服务注册完 + 稳定期。

    settle 默认 15 秒：服务在了不等于 UI 能点。开机后马上点，
    会撞在首屏动画和资源加载上，点了没反应。
    """
    try:
        sys.path.insert(0, HERE)
        import airlib
        return airlib.wait_system_ready(SERIAL, max_wait=max_wait, settle=settle)
    except Exception as e:
        return False, "等系统就绪失败（%s），跳过" % e


def _run_once(url, steps, tag=""):
    """调一次 /flow/run，返回 (全通过?, 结果dict)。"""
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "600", "-X", "POST", url + "/flow/run",
             "-H", "Content-Type: application/json",
             "-d", __import__("json").dumps(
                 {"steps": steps, "tries": 8, "interval": 0.8,
                  "retry": 3, "resume_rounds": 3}, ensure_ascii=False)],
            capture_output=True, text=True, timeout=620).stdout
        r = __import__("json").loads(out)
    except Exception as e:
        log("调用流程接口失败: %s" % e)
        return False, {}

    acc = 0
    for rec in r.get("results", []):
        ok = rec.get("action_ok") and rec.get("verify_ok") is not False
        acc += int(rec.get("ms") or 0)
        # 这些行是流程跑完后一次性补打的，时间戳都一样。额外标出"到这一步
        # 累计用了多少秒"，才能看出时间花在哪、哪一步变慢了。
        log("  %s %s. %s (%sms, 累计%.1fs)" % (
            "OK " if ok else "FAIL", rec.get("i"), rec.get("desc"),
            rec.get("ms"), acc / 1000.0))
    for x in r.get("resume_log", []):
        log("  [恢复] 第%d步失败 → %s" % (x.get("failed_at"), x.get("did")))
    log("流程结束：%s  用时 %sms  %s/%s 步通过（轮次 %s）" % (
        "全部通过" if r.get("all_pass") else "有失败",
        r.get("ms"), r.get("done"), r.get("total"), r.get("rounds")))
    return bool(r.get("all_pass")), r


def run_flow():
    """调 airweb 跑流程，最后用视觉模型确认屏幕真的在共享。

    只信模板匹配不够：实测出现过 7/7 全过但 dumpsys 里没有投屏会话，
    孩子那边是黑的。所以跑完必须过 AI 验证；不过就重跑。
    """
    url = os.environ.get("AIRWEB", "http://127.0.0.1:8123")
    flow = os.environ.get("FLOW", os.path.join(HERE, "flows", "通话投屏.json"))
    try:
        with open(flow) as f:
            steps = __import__("json").load(f).get("steps") or []
    except Exception as e:
        log("流程文件读不了: %s" % e)
        return False
    # 手机可能刚开机，等它真的起来再动手
    ok_bat, why_bat = battery_ok()
    log(why_bat)
    if not ok_bat:
        log(">>> 跳过本次：先充电")
        return False

    boot_ok = wait_android_boot()
    # boot_completed=1 只是"启动流程走完"，system_server 的服务可能还在注册。
    # 实测踩过：流程开跑时 `screencap` 报 Can't find service: window，
    # 第 4 步直接 BrokenPipeError。所以再等系统服务真的能用。
    if boot_ok:
        ok, why = wait_system_ready_here()
        log(why)
        if not ok:
            log("系统没就绪，仍然试着跑（比直接放弃强）")
    # 跑 + AI 验证 + 不过则重跑。
    # 为什么要重跑：实测第 4 步（点 打电话）会偶发失败，手动点同位置立刻成功，
    # 说明不是坐标问题而是手机当下的状态问题，重跑一次基本就好了。
    MAX_ATTEMPTS = int(os.environ.get("FLOW_ATTEMPTS", "3"))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            log(">>> 第 %d 次尝试" % attempt)
            # 上一轮可能停在任意界面，退回桌面再来
            try:
                subprocess.run(["adb", "-s", SERIAL, "shell", "input", "keyevent",
                                "KEYCODE_HOME"], capture_output=True, timeout=15)
                time.sleep(2)
            except Exception:
                pass

        log(">>> 开始执行流程（%d 步）" % len(steps))
        flow_ok, r = _run_once(url, steps)
        log(">>> 模板流程%s" % ("通过" if flow_ok else "有失败"))

        if not flow_ok:
            # 模板流程失败时交给 smartcast：它同样用模板找按钮，但会叠加
            # 大模型判断页面、坐标核对、以及「点错了就拉回首页重来」。
            # 实测模板流程卡在会话详情页时（底部没有打电话），融合路径
            # 能自己回首页并点对，所以这是有意义的第二道路径。
            log(">>> 模板流程没成，切 AI 融合路径")
            try:
                import smartcast
                sr = smartcast.run(SERIAL, log=lambda m: log("    [cast] " + m))
                log(">>> AI 融合路径：%s（%s）" % ("成功" if sr["ok"] else "失败",
                                              sr.get("why", "")))
                flow_ok = flow_ok or bool(sr["ok"])
            except Exception as e:
                log(">>> AI 融合路径出错: %s" % str(e)[:120])

        # 模板匹配说通过不算数，要看屏幕真的在共享。
        try:
            import aiverify
            vr = aiverify.verify(SERIAL)
            log(">>> AI 验证：%s（%s）" % ("通过" if vr["ok"] else "未通过",
                                          vr.get("reason", "")))
            if vr.get("llm_evidence"):
                log("    模型看到：%s" % vr["llm_evidence"])
        except Exception as e:
            log("AI 验证出错（%s），按流程结果判定" % e)
            vr = {"ok": flow_ok}

        if flow_ok and vr.get("ok"):
            log(">>> 执行成功（第 %d 次尝试，已确认屏幕正在共享）" % attempt)
            return True

        if attempt < MAX_ATTEMPTS:
            log(">>> 没成，%.0fs 后再来一次" % 8)
            time.sleep(8)

    log(">>> 执行失败：试了 %d 次都没能确认投屏" % MAX_ATTEMPTS)
    return False


def cast_owner_now():
    """快速看现在有没有在共享屏幕，返回 owner 包名或空串。

    不用截图也不用问模型，只查一次 dumpsys，几百毫秒。守护循环要每
    20 秒查一次，不能每次都截图+叫模型（那会烧钱也会拖慢）。
    """
    dev = any_device()
    if not dev:
        return ""
    try:
        out = subprocess.run(
            ["adb", "-s", dev, "shell", "dumpsys display | grep ScreenCapture"],
            capture_output=True, text=True, timeout=20).stdout or ""
    except Exception:
        return ""
    for line in out.splitlines():
        if "owner" in line:
            seg = line.split("owner", 1)[1].split()
            return seg[0] if seg else ""
    return ""


def main():
    once = "--once" in sys.argv
    if once:
        log("手机%s" % ("已插入: " + str(find_phone()) if find_phone() else "未插入"))
        return 0
    log("盯 USB 口：vendor=%s serial=%s（防抖 %.1fs）" % (VENDOR, SERIAL, DEBOUNCE))
    log("等 airweb 就绪…")
    url = os.environ.get("AIRWEB", "http://127.0.0.1:8123")
    # 探测用 "/"（永远 200），不能用 "/power"。
    # 实测踩过：/power 内部要 connect_device，手机不在 USB 上时抛
    # DeviceConnectionError → 500。板子刚通电那一刻手机往往还没枚举完，
    # 于是这里判"airweb 没起来"，usbwatch 直接退出——而它正是要在
    # 手机插上时干活的那个进程，退出了就再也不会有任何反应。
    # 更不能"等不到就退出"：systemd 那边是 oneshot+RemainAfterExit，
    # 进程死了不会有人重启它。所以这里无限等下去。
    waited = 0
    while True:
        rc = subprocess.run(["curl", "-s", "-m", "2", "-o", "/dev/null", "-w",
                             "%{http_code}", url + "/"],
                            capture_output=True, text=True).stdout.strip()
        if rc == "200":
            break
        if waited and waited % 60 == 0:
            log("还在等 airweb（已等 %ds）" % waited)
        waited += 1
        time.sleep(1)
    log("airweb 就绪")

    present = find_phone() is not None
    log("当前状态：%s" % ("已插着" if present else "没插"))
    last_run = 0.0

    # 投屏守护。
    #
    # 实测（logcat 两轮，精确到秒）：
    #   11-13 06:50:04 建立 → 06:55:04 销毁 = 5分00秒
    #   11-13 15:58:22 建立 → 16:03:22 销毁 = 5分00秒
    # 豆包的通话到 5 分钟就自己结束（audio xlog 里 communication_time:306
    # 出现 3 次），投屏跟着被停。所以"跑成功一次"不等于"孩子一直能看到"：
    # 必须盯着 owner，没了就重新拉起。
    CAST_WATCH = os.environ.get("CAST_WATCH", "1") == "1"
    CAST_CHECK_EVERY = float(os.environ.get("CAST_CHECK_EVERY", "20"))
    cast_up = False            # 我们认为现在应该在投屏吗
    next_cast_check = 0.0

    def rearm(reason):
        """跑一轮流程，并把 cast_up 更新成真实状态。"""
        nonlocal last_run, cast_up, next_cast_check
        log(">>> %s" % reason)
        last_run = time.time()
        try:
            ok = run_flow()
        except Exception as e:
            log("跑流程出错: %s" % e)
            ok = False
        cast_up = bool(cast_owner_now())
        next_cast_check = time.time() + CAST_CHECK_EVERY
        if ok and not cast_up:
            log("流程说成功但 owner 已经没了（可能刚好卡在 5 分钟边界），下一轮再拉")
        return ok

    # 板子开机时手机通常已经插着（用户给板子通电、手机靠 USB 自动开机）。
    # 这种情况下永远等不到"拔→插"的跳变，所以启动时插着就先跑一次。
    if present:
        rearm("启动时已插着，先跑一次")

    while True:
        now = find_phone() is not None
        if now != present:
            # 防抖：等电平稳下来，手机枚举会抖几下
            time.sleep(DEBOUNCE)
            now2 = find_phone() is not None
            if now2 == now:
                present = now
                if now:
                    log("已插上 USB")
                    if time.time() - last_run < COOLDOWN:
                        log("还在冷却期（%.0fs），跳过" % COOLDOWN)
                    else:
                        rearm("新插入，开始跑")
                else:
                    log("已拔掉 USB")
                    cast_up = False

        # 投屏守护：每 CAST_CHECK_EVERY 秒查一次 owner。
        # 只在手机插着、且我们以为应该在投屏时才查——手机不在时查也没用。
        if CAST_WATCH and present and time.time() >= next_cast_check:
            next_cast_check = time.time() + CAST_CHECK_EVERY
            owner = cast_owner_now()
            if owner:
                if not cast_up:
                    log("投屏在线（owner %s）" % owner)
                cast_up = True
            elif cast_up:
                # 刚才是好的，现在没了 —— 豆包 5 分钟限制到点。
                log("投屏不在了（owner 空），豆包限量到点，重新拉起")
                cast_up = False
                time.sleep(2)
                rearm("掉线重拉")

        time.sleep(0.25)


if __name__ == "__main__":
    sys.exit(main())
