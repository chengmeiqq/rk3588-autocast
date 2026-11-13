#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆包投屏守护：投屏掉了就重新拉起来。

为什么需要它（实测证据，不是猜的）：
  1. MIUI 的「一键清理」会直接杀进程。logcat 里是
     `Force stopping com.larus.nova ... SwipeUpClean`。
  2. MIUI 的 SmartPower 会回收后台：
     `SmartPower: com.larus.nova/10231 state=inactive adj=0 move to died`。
  3. 豆包自己有 5 分钟硬上限（61 次会话里 4 次精确 5分00秒），
     软件绕不过，只能挂断后重拉。
  这三种情况的共同点是：**投屏静默消失，界面上什么都没有**。
  人不在旁边就发现不了，所以要有东西替我们盯着。

守什么（判断依据，两把尺子都要动）：
  - `casting_owner`：虚拟屏 + 采集服务前台双判据
    （只看虚拟屏会被残留骗：进程在、屏残留、投屏已死，实测发生过）
  - `pidof com.larus.nova`：豆包进程还活着吗
  只有两个都成立才算健康。只看进程不行 —— 进程在、投屏被掐掉的情况
  真实发生过；只看投屏也不行 —— 会话对象残留时进程可能已经死了。

不做什么（边界，很重要）：
  - 不在用户手动暂停时抢活：`state/keepcast.pause` 存在就完全不动。
  - 不和 Tasker 的 `/cast` 抢：两边共用 `/tmp/_cast.lock`，
    谁先拿到谁跑，另一个当场让路（不排队、不叠加点击）。
  - 手机拔掉/没连 adb 时只记一句，不刷屏、不疯狂重试。
"""
import fcntl
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smartcast                                    # noqa: E402

STATE_DIR = os.path.join(HERE, "state")
STATE = os.path.join(STATE_DIR, "keepcast.json")
PAUSE = os.path.join(STATE_DIR, "keepcast.pause")
LOG = os.path.join(HERE, "logs", "keepcast.log")

ADB = "/usr/lib/android-sdk/platform-tools/adb"
APP = os.environ.get("PHONE_APP", "com.larus.nova")

CHECK_EVERY = float(os.environ.get("KEEPCAST_INTERVAL", "20"))
# 连续失败后的退避上限，防止手机被拔掉时每 20 秒锤一次 adb
BACKOFF_MAX = 300.0

# 手机插着的判据：USB 设备节点在不在。
# 为什么看 sysfs 而不看 adb：
#   这手机重新枚举时会变成 2717:ff40/ff48（纯 MTP，无 adb 接口），
#   这时 `adb devices` 是空的，但"线插着"这个事实没变。
#   用 adb 判断会把"插着但 adb 掉了"误判成"没插"。
USB_ROOT = "/sys/bus/usb/devices"
USB_SERIAL = os.environ.get("PHONE_SERIAL", "892e20c9")
USB_VENDORS = ("2717", "18d1")     # 小米 / Google(adb)，两种都认


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 2_000_000:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_state(**kw):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        old = {}
        if os.path.exists(STATE):
            try:
                old = json.load(open(STATE, encoding="utf-8"))
            except Exception:
                old = {}
        old.update(kw)
        old["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass


def usb_present():
    """手机插在板子上了吗。返回 (bool, 说明)。

    只看 USB 设备节点，不问 adb —— 理由见文件头常量处的注释。
    串号对得上就算；对不上但厂商是小米/Google 的也认（换线改串号不影响）。
    """
    try:
        names = os.listdir(USB_ROOT)
    except OSError:
        return False, "读不到 %s" % USB_ROOT

    for n in names:
        d = os.path.join(USB_ROOT, n)
        vp = os.path.join(d, "idVendor")
        if not os.path.isfile(vp):
            continue                       # 接口节点（2-1.1:1.0）没有 idVendor
        try:
            with open(vp) as f:
                vid = f.read().strip()
        except OSError:
            continue
        if vid not in USB_VENDORS:
            continue
        sn = ""
        sp = os.path.join(d, "serial")
        if os.path.isfile(sp):
            try:
                with open(sp) as f:
                    sn = f.read().strip()
            except OSError:
                sn = ""
        if USB_SERIAL and sn and sn != USB_SERIAL:
            continue                       # 别把别的设备当手机
        return True, "%s(%s:%s)" % (n, vid, sn or "无串号")

    return False, "没找到手机 USB 节点"


def proc_alive(serial):
    """豆包进程还在吗。返回 pid 或 0。"""
    try:
        p = subprocess.run([ADB, "-s", serial, "shell", "pidof " + APP],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=12)
        out = p.stdout.decode("utf-8", "replace").strip()
        return int(out.split()[0]) if out else 0
    except Exception:
        return 0


def device_ready(serial):
    """手机此刻能不能跑 adb 命令（拔掉/掉线时别硬拉）。"""
    try:
        p = subprocess.run([ADB, "-s", serial, "shell", "echo _OK_"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10)
        return "_OK_" in p.stdout.decode("utf-8", "replace")
    except Exception:
        return False


def run_locked(serial):
    """跑一次完整投屏流程。lock 拿不到就说明别人在跑，直接让路。"""
    lock_path = getattr(smartcast, "CAST_LOCK", "/tmp/_cast.lock")
    lf = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None                      # 有别的流程在跑，本轮跳过
        # 直接调内层：外层 run() 会再抢同一把锁，
        # 同进程里两个不同的 fd 会互相认为"别人在跑"，必须绕过。
        return smartcast._run_locked(serial=serial, log=log)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()


USB_DLG_EVERY = float(os.environ.get("USB_DLG_INTERVAL", "2.5"))
USB_DLG_TEXTS = ("传输文件", "文件传输")
USB_DLG_ACT = ("UsbDetailsActivity", "UsbModeChooser")


def _click_transfer(serial):
    """把「USB 用于」框里的「传输文件」点掉。返回点到的节点或 None。

    要点文字节点而不是容器：这三个选项在无障碍树里 clickable=false，
    真正接收点击的是外层容器，它覆盖着文字中心，点文字坐标照样生效。
    这个坑最早让我空转 150 秒。
    """
    import uitap
    ns = uitap.dump(serial)
    lb = " ".join((n["text"] + " " + n["desc"]) for n in ns)
    if not any(t in lb for t in USB_DLG_TEXTS):
        return None
    for t in USB_DLG_TEXTS:
        if t in lb:
            return uitap.click(serial, t, exact=True,
                               clickable_only=False, wait=1.5)
    return None


TASKER = os.environ.get("TASKER_PKG", "net.dinglisch.android.taskerm")
TASKER_ACT = TASKER + "/.Tasker"     # 正确 launcher：.MainActivity/.Main 都不存在
TASKER_EVERY = float(os.environ.get("TASKER_INTERVAL", "20"))
USB_RX = ("com.android.settings/.connecteddevice.usb."
          "UsbModeChooserReceiver")


def ensure_usb_receiver_disabled(serial, quiet=False):
    """把「USB 用于」那个 receiver 停掉，避免它反复把框拉出来。

    MIUI 的 UsbModeChooserReceiver 注册了 USB_STATE / USB_DEVICE_ATTACHED /
    USB_DEVICE_DETACHED / MEDIA_MTP_TRANSFER_FAILED / BOOT_COMPLETED 五个广播，
    每收到一个就 startActivity 拉起 Settings$UsbDetailsActivity。
    实测 20:36:08、20:36:58 连续两次把它拉出来，框就杵在屏幕上挡点击。

    为什么不禁用整个 com.android.settings：那是设置应用本体，禁用会让
    设置功能整个消失。只禁这一个 receiver 组件，设置仍能正常打开。

    幂等：重复 disable 只是再报一次 "new state: disabled"，不会出错。
    所以不用先查询状态（这系统上 pm get-component-enabled-state 不存在），
    每次启动无条件执行一次即可，系统升级把它重置了也能自愈。
    """
    try:
        p = subprocess.run(
            [ADB, "-s", serial, "shell",
             'su -c "pm disable --user 0 %s"' % USB_RX],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=25)
        out = p.stdout.decode("utf-8", "replace")
        if "disabled" in out:
            if not quiet:
                log("USB 用途框：receiver 已是 disabled（不会自己弹了）")
            return True
        log("USB 用途框：receiver 禁用失败 → %s" % out.strip()[:80])
        return False
    except Exception as e:
        log("USB 用途框：receiver 禁用出错 → %s" % str(e)[:80])
        return False


def tasker_alive(serial):
    """Tasker 进程还在吗。返回 pid 或 0。"""
    try:
        p = subprocess.run([ADB, "-s", serial, "shell", "pidof " + TASKER],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=12)
        out = p.stdout.decode("utf-8", "replace").strip()
        return int(out.split()[0]) if out else 0
    except Exception:
        return 0


def tasker_watch():
    """MIUI 会把 Tasker 也杀掉，这里替它续命。

    实证：20:05:13 SmartPower 把 Tasker 干掉
      net.dinglisch.android.taskerm/10283(9743): background->died(468486ms)
      R(process died) adj=200
    然后 17 分钟没人发现，直到手动 am start 才活过来。

    查过但没用的手段：
      - 电池优化白名单：已在里面，照样被杀
      - powerkeeper 的 bgControl：已经是 noRestrict，照样被杀
      - manual_list 的 white_list（自启动白名单）：整张表是空的
      - Magisk 模块：没有可靠口碑的「防 MIUI 杀进程」模块，刷了风险大
    所以走板子侧看门狗，不动系统。

    为什么不复查进程时先查 USB：拔线时手机被单独拿着用，
    这时候去拉起 Tasker 纯属添乱，和投屏守护同一个门禁。
    """
    time.sleep(6.0)                   # 等主循环先跑一轮，别抢 adb
    last_note = None
    while True:
        try:
            if os.path.exists(PAUSE) or not usb_present()[0]:
                time.sleep(TASKER_EVERY * 2)
                continue

            serial = smartcast.pick_serial()
            if not device_ready(serial):
                time.sleep(TASKER_EVERY)
                continue

            pid = tasker_alive(serial)
            if pid:
                if last_note != "alive":
                    last_note = "alive"
                    save_state(tasker_pid=pid)
                time.sleep(TASKER_EVERY)
                continue

            log("Tasker 不在（被 MIUI 杀了），重新拉起")
            try:
                subprocess.run([ADB, "-s", serial, "shell", "am start -n "
                                + TASKER_ACT],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=25)
            except Exception as e:
                log("拉起 Tasker 出错：%s" % str(e)[:60])
            time.sleep(8)             # 给它启动时间，下一轮再确认
            pid = tasker_alive(serial)
            if pid:
                log("Tasker 已重新拉起（pid=%d）" % pid)
                save_state(tasker_pid=pid, tasker_note="已重新拉起")
                last_note = "revived"
            else:
                log("Tasker 拉起后仍然没有进程（可能启动很慢）")
                save_state(tasker_note="拉起失败")
                last_note = "fail"
        except Exception as e:
            log("Tasker 监视出错（继续）：%s" % str(e)[:80])
        time.sleep(TASKER_EVERY)


BOOT_ID_HINT = os.path.join(STATE_DIR, "phone_boot_id")


def phone_boot_id(serial):
    """手机这次开机的唯一 ID。读不到返回空串。"""
    try:
        p = subprocess.run(
            [ADB, "-s", serial, "shell", "cat /proc/sys/kernel/random/boot_id"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12)
        t = p.stdout.decode("utf-8", "replace").strip()
        return t if len(t) > 8 else ""
    except Exception:
        return ""


def check_receiver_after_boot(serial):
    """手机重启过就把 USB 框那个 receiver 重新禁掉。

    为什么用 boot_id 而不是定时复查：
      boot_id 每次开机都变，是最便宜的"手机重启过没有"的判据。
      定时每轮都 su 一次纯属浪费电，而且多数时候什么都没变。
      只在 boot_id 变化时才动一次 su，代价几乎为零。

    已知边界：手机重启后如果一直没插回来，这里就查不到；
      等它插回来，第一轮比对 boot_id 就会立刻补上。
    """
    bid = phone_boot_id(serial)
    if not bid:
        return

    old = ""
    try:
        with open(BOOT_ID_HINT) as f:
            old = f.read().strip()
    except OSError:
        old = ""

    if bid == old:
        return

    if old:
        log("手机重启过（boot_id 变了），重新禁用 USB 用途框 receiver")
    else:
        log("首次记录手机 boot_id=%s，校正一次 receiver 状态" % bid[:8])

    ensure_usb_receiver_disabled(serial, quiet=not old)
    try:
        with open(BOOT_ID_HINT, "w") as f:
            f.write(bid + "\n")
    except OSError:
        pass


def usb_dialog_watch():
    """专门盯「USB 用于」这个框，看到就点「传输文件」。

    为什么需要单独的线程（而不是塞进主循环）：
      MIUI 的 UsbModeChooserReceiver 注册了 USB_STATE / USB_DEVICE_ATTACHED /
      BOOT_COMPLETED 三个广播，**每次 USB 配置变化都会把这个框重新拉出来**。
      logcat 实证：
        UsbModeChooserReceiver: action = android.hardware.usb.action.USB_STATE
        START u0 {cmp=com.android.settings/.Settings$UsbDetailsActivity}
      主循环 20 秒一轮，而且投屏健康时根本不会去动它 —— 框就杵在屏幕上，
      挡住后面所有点击，用户还以为"选项没生效"。

    为什么先看 usb_present()：拔线后这个框不存在，没必要每 2.5 秒问一次 adb。
    """
    time.sleep(2.0)
    while True:
        try:
            if not usb_present()[0]:
                time.sleep(USB_DLG_EVERY * 4)
                continue

            serial = smartcast.pick_serial()

            # 手机重启后 receiver 可能被系统重置回来，这里顺手校正。
            # 只有 boot_id 变了才真的去 su，平时零开销。
            try:
                check_receiver_after_boot(serial)
            except Exception as e:
                log("boot_id 校正出错（继续）：%s" % str(e)[:60])

            fg = smartcast.foreground(serial)
            if not any(a in fg for a in USB_DLG_ACT):
                time.sleep(USB_DLG_EVERY)
                continue

            # 主流程正在跑就让它自己处理，别两边同时点
            lock_path = getattr(smartcast, "CAST_LOCK", "/tmp/_cast.lock")
            with open(lock_path, "w") as lf:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    time.sleep(USB_DLG_EVERY)
                    continue
                try:
                    got = _click_transfer(serial)
                    if got:
                        log("USB 用途框：已点「%s」"
                            % ((got.get("text") or got.get("desc") or "?")[:10]))
                        save_state(usb_dialog="已点掉")
                    else:
                        log("USB 用途框在前台，但没找到「传输文件」（界面可能变了）")
                        save_state(usb_dialog="没找到按钮")
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception as e:
            log("USB 框监视出错（继续）：%s" % str(e)[:80])
        time.sleep(USB_DLG_EVERY)


def main():
    serial = smartcast.pick_serial()
    log("守护启动：设备=%s  检查间隔=%.0fs  暂停开关=%s"
        % (serial, CHECK_EVERY, PAUSE))

    import threading
    threading.Thread(target=usb_dialog_watch, daemon=True).start()
    log("USB 用途框监视已启动：每 %.1fs 查一次，看到就点「传输文件」"
        % USB_DLG_EVERY)

    # 启动时补一次 receiver 禁用：系统升级/恢复出厂会把它重置回来，
    # 这里无条件再执行一次（幂等），保证开机后不用人工干预。
    if usb_present()[0] and device_ready(serial):
        ensure_usb_receiver_disabled(serial)
    else:
        log("USB 用途框：此刻手机不可达，receiver 禁用检查延后")

    threading.Thread(target=tasker_watch, daemon=True).start()
    log("Tasker 存活监视已启动：每 %.0fs 查一次，被杀就 am start" % TASKER_EVERY)

    fails = 0
    started_at = 0.0          # 本次投屏开始时间，用来记会话时长
    was_ok = None

    while True:
        try:
            if os.path.exists(PAUSE):
                if was_ok is not False:
                    log("已暂停（%s 存在），不动手" % os.path.basename(PAUSE))
                    save_state(paused=True, ok=False, note="已暂停")
                    was_ok = False
                time.sleep(CHECK_EVERY)
                continue

            # ── 第一道门：USB 在不在 ──
            # 用户要求：插着 USB 才守，拔了就不干活。
            # 插着意味着手机靠板子供电、能被 adb 到、随时可能被清理掉；
            # 拔了手机在被单独拿着用，我们去点屏幕只会添乱。
            # 这里在调 adb 之前就判断，拔线时一条 adb 命令都不发。
            plugged, why_usb = usb_present()
            if not plugged:
                # was_ok 用两种值区分"从没跑过"和"跑过但现在待命"，
                # 但日志只打一次：启动时就拔着线，也要说一句在待命，
                # 否则看日志会误以为进程没起来。
                if was_ok is not False:
                    log("USB 没插（%s），守护待命，插上就自动接管" % why_usb)
                    save_state(ok=False, note="USB 未连接，守护待命",
                               plugged=False, casting=False, fails=0,
                               serial=USB_SERIAL)
                    was_ok = False
                    started_at = 0.0
                time.sleep(CHECK_EVERY)
                continue

            serial = smartcast.pick_serial()

            if not device_ready(serial):
                fails += 1
                wait = min(BACKOFF_MAX, CHECK_EVERY * (2 ** min(fails, 4)))
                log("USB 在但手机没连上 adb（%s），%.0fs 后再看" % (serial, wait))
                save_state(ok=False, serial=serial, note="USB 在但 adb 不通",
                           fails=fails, pid=0, plugged=True)
                time.sleep(wait)
                continue

            owner = smartcast.casting_owner(serial)
            pid = proc_alive(serial)
            healthy = bool(owner) and bool(pid)

            if healthy:
                if was_ok is not True:
                    log("健康：投屏中（owner=%s, pid=%d）" % (owner, pid))
                    was_ok = True
                started_at = started_at or time.time()
                save_state(ok=True, serial=serial, owner=owner, pid=pid,
                           note="投屏中", fails=0, paused=False, plugged=True,
                           since=time.strftime("%H:%M:%S",
                                               time.localtime(started_at)),
                           uptime=int(time.time() - started_at),
                           # why 必须显式清掉：save_state 是 dict.update 语义，
                           # 不传就会把历史上那次失败的原因一直留着，
                           # 状态看起来正常、why 却写着"投屏没了"，非常误导。
                           why="")
                fails = 0
                time.sleep(CHECK_EVERY)
                continue

            # ── 这里开始：掉线了 ──
            why = []
            if not pid:
                why.append("豆包进程没了")
            if not owner:
                why.append("投屏没了")
            log("掉线（%s）：owner=%r pid=%d → 重新拉起"
                % ("、".join(why), owner, pid))
            started_at = 0.0

            r = run_locked(serial)
            if r is None:
                log("已有别的投屏流程在跑，本轮让路")
                was_ok = False
                time.sleep(CHECK_EVERY)
                continue

            if r.get("ok"):
                fails = 0
                started_at = time.time()
                log("已重新拉起：%s" % r.get("why", ""))
                save_state(ok=True, serial=serial,
                           owner=smartcast.casting_owner(serial),
                           pid=proc_alive(serial),
                           note="刚重新拉起", fails=0,
                           since=time.strftime("%H:%M:%S",
                                               time.localtime(started_at)),
                           uptime=0)
                was_ok = True
            else:
                fails += 1
                wait = min(BACKOFF_MAX, CHECK_EVERY * (2 ** min(fails, 4)))
                log("拉起失败（第 %d 次）：%s → %.0fs 后重试"
                    % (fails, r.get("why", "")[:100], wait))
                save_state(ok=False, serial=serial, note="拉起失败",
                           why=r.get("why", ""), fails=fails)
                was_ok = False
                time.sleep(wait)
                continue

        except Exception as e:
            log("循环出错（继续跑）：%s" % e)
            fails += 1

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    if "--once" in sys.argv:
        s = smartcast.pick_serial()
        print("device_ready:", device_ready(s))
        print("owner:", smartcast.casting_owner(s))
        print("pid:", proc_alive(s))
    else:
        main()
